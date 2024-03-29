#ifndef PIO_ENCODER_H
#define PIO_ENCODER_H
#include <pico/stdlib.h>
#include <pio_encoder.pio.h>

class PIOEncoder
{
public:

    PIOEncoder(PIO pio, uint32_t state_machine_id,
               uint8_t base_input_pin);

    ~PIOEncoder();

    //void setup_dma_stream_to_memory(uint32_t* address);

    void request_count();

    uint32_t fetch_count();

/**
 * \brief return the encoder count. This takes around ~11 CPU cycles.
 */
    uint32_t get_count();


private:

    PIO pio_;
    uint32_t sm_;
    uint32_t pin_a_;
    uint32_t pin_b_;

};
#endif // PIO_ENCODER_H
